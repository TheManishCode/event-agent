"""Quote-request drafts (ADR-041, F11 without sending): EventMate writes the message and builds a link; the user sends it.

Nothing here sends anything, uses the WhatsApp Business API or automates a WhatsApp account.
"""

import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from pydantic import ValidationError

from eventmate.messaging.outreach import normalise_phone
from eventmate.schemas import VendorCategory as C
from eventmate.service import EventMate
from tests.conftest import EXAMPLE, TODAY
from eventmate.web.app import create_app


@pytest.fixture
def em(tmp_path):
    return EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="out")


@pytest.fixture
def pid(em):
    return em.plan(EXAMPLE, today=TODAY).plan.plan_id


NO_PHOTO = "Plan a 120-person engagement party in Pune on December 12 under INR 200,000. Vegetarian catering, indoor venue."


@pytest.fixture
def pid_nophoto(em):
    return em.plan(NO_PHOTO, today=TODAY).plan.plan_id


def mail(em, pid, **kw):
    base = dict(category=C.CATERER, channel="email", recipient="sales@annapurna.in", sender_name="Asha")
    return em.draft_outreach(pid, **{**base, **kw})


def wa(em, pid, **kw):
    return mail(em, pid, channel="whatsapp", recipient="+91 98765 43210", **kw)


def test_email_draft_has_a_mailto_link_that_round_trips(em, pid):
    d = mail(em, pid)
    parts = urlsplit(d.link)
    assert d.link.startswith("mailto:sales@annapurna.in?") and parts.scheme == "mailto"
    q = parse_qs(parts.query)
    assert q["subject"] == [d.subject] and q["body"] == [d.body]
    plan = em.store.latest(pid)
    assert plan.selection_for(C.CATERER).name in d.body
    assert str(plan.request.guest_count) in d.body and plan.request.city in d.body and plan.request.event_date.isoformat() in d.body
    assert "service staff" in d.body and "valid" in d.body  # asks for the checklist and the validity period


def test_whatsapp_draft_uses_click_to_chat_not_an_api(em, pid):
    d = wa(em, pid)
    assert d.link.startswith("https://wa.me/919876543210?text=") and d.recipient == "919876543210"
    assert unquote(d.link.split("text=", 1)[1]) == d.body and d.subject == ""
    assert "until you approve" in d.notice


def test_the_budget_is_never_shared_with_a_vendor(em, pid):
    plan = em.store.latest(pid)
    for d in (mail(em, pid), wa(em, pid)):
        assert "budget" not in d.body.lower() and str(plan.request.budget_total) not in d.body.replace(",", "")
        assert str(plan.total_cost) not in d.body.replace(",", "")


@pytest.mark.parametrize("raw,want", [
    ("+91 98765 43210", "919876543210"), ("9876543210", "919876543210"), ("+1 (415) 555-2671", "14155552671"), ("0091-98765-43210", "919876543210"),
])
def test_phone_numbers_are_normalised(raw, want):
    assert normalise_phone(raw) == want


@pytest.mark.parametrize("bad", ["", "12345", "98765", "abcdefghij", "+0 123456789", "9876543210; rm -rf /", "+91 98765 43210\n+91 1", "1" * 20, "5876543210", "https://evil"])
def test_bad_phone_numbers_fail_closed(bad):
    with pytest.raises(ValueError):
        normalise_phone(bad)


@pytest.mark.parametrize("bad", [
    "nobody", "a@b", "a@b.com,c@d.com", "a@b.com\nbcc:x@y.com", "a b@c.com", "a@b.com?cc=x@y.com", "a@b.com&bcc=x@y.com", "<a@b.com>", "x" * 300 + "@b.com", "",
])
def test_bad_email_addresses_are_refused(em, pid, bad):
    with pytest.raises((ValueError, ValidationError)):
        mail(em, pid, recipient=bad)


def test_hostile_sender_name_cannot_add_mail_parameters_or_lines(em, pid):
    d = mail(em, pid, sender_name="Asha&bcc=evil@x.com#")
    q = parse_qs(urlsplit(d.link).query)
    assert set(q) == {"subject", "body"} and "bcc" not in d.link.split("?", 1)[0]
    for bad in ("Asha\nBcc: x@y.com", "x\x00", "e\x1b[31m", "n" * 81, ""):
        with pytest.raises((ValueError, ValidationError)):
            mail(em, pid, sender_name=bad)


def test_the_category_must_be_in_the_plan(em, pid_nophoto):
    with pytest.raises(ValueError, match="not in this plan"):
        mail(em, pid_nophoto, category=C.PHOTOGRAPHER)


def test_unknown_plan_and_unknown_channel_fail(em, pid):
    with pytest.raises(FileNotFoundError):
        em.draft_outreach("nope", category=C.CATERER, channel="email", recipient="a@b.in", sender_name="A")
    with pytest.raises((ValueError, ValidationError)):
        mail(em, pid, channel="sms")


def test_drafts_persist_per_plan_privately_and_never_change_the_plan(em, pid, tmp_path):
    before = (em.store.latest(pid).total_cost, em.ledger_summary(pid).committed_total, len(em.store.versions(pid)) if hasattr(em.store, "versions") else 0)
    d = mail(em, pid)
    assert em.outreach(pid) == [d] and d.status == "prepared"
    after = (em.store.latest(pid).total_cost, em.ledger_summary(pid).committed_total, len(em.store.versions(pid)) if hasattr(em.store, "versions") else 0)
    assert before == after and em.revalidate(pid).passed
    other = em.plan(EXAMPLE.replace("120", "90"), today=TODAY).plan.plan_id
    assert em.outreach(other) == []


def test_marking_sent_is_the_users_statement_and_is_append_only(em, pid):
    d = mail(em, pid)
    done = em.mark_outreach_sent(pid, d.draft_id)
    assert done.status == "marked_sent" and done.marked_at is not None and done.body == d.body
    assert [x.status for x in em.outreach(pid)] == ["marked_sent"]  # latest state per draft
    assert em.mark_outreach_sent(pid, d.draft_id).status == "marked_sent"  # idempotent
    with pytest.raises(ValueError, match="unknown draft"):
        em.mark_outreach_sent(pid, "f" * 32)
    lines = (em.store.root / "outreach" / f"outreach-{pid}.jsonl").read_text().splitlines()
    assert len(lines) == 2  # the original draft line is still there


def test_the_draft_model_refuses_a_link_that_is_not_mailto_or_wa_me():
    from eventmate.schemas import OutreachDraft
    base = dict(draft_id="a", plan_id="p", created_at="2026-10-30T00:00:00Z", category="caterer", vendor_id="v", vendor_name="V", channel="email",
                recipient="a@b.in", subject="s", body="b", link="mailto:a@b.in?subject=s")
    OutreachDraft(**base)
    for bad in ("javascript:alert(1)", "https://evil.example/", "https://wa.me.evil.com/1", "mailto:a@b.in\nbcc:x"):
        with pytest.raises(ValidationError):
            OutreachDraft(**{**base, "link": bad})


def test_hostile_vendor_text_cannot_reach_the_message(em, pid):
    d = mail(em, pid)
    assert "ignore" not in d.body.lower()


# ---- API

@pytest.fixture
def client(em):
    return create_app(em, today_fn=lambda: TODAY).test_client()


def post(client, url, body):
    token = client.get("/api/session").get_json()["csrf"]
    return client.post(url, data=json.dumps(body), headers={"X-EventMate-CSRF": token, "Content-Type": "application/json"})


BODY = {"category": "caterer", "channel": "whatsapp", "recipient": "9876543210", "sender_name": "Asha"}


def test_api_creates_lists_and_marks(client, pid):
    r = post(client, f"/api/plans/{pid}/outreach", BODY)
    assert r.status_code == 201, r.get_json()
    draft = r.get_json()
    assert draft["link"].startswith("https://wa.me/919876543210?text=")
    assert client.get(f"/api/plans/{pid}/outreach").get_json()[0]["draft_id"] == draft["draft_id"]
    m = post(client, f"/api/plans/{pid}/outreach/{draft['draft_id']}/sent", {})
    assert m.status_code == 200 and m.get_json()["status"] == "marked_sent"


def test_api_rejects_bad_input_and_needs_csrf(client, pid, pid_nophoto):
    assert post(client, f"/api/plans/{pid}/outreach", {**BODY, "recipient": "123"}).status_code in (400, 422)
    assert post(client, f"/api/plans/{pid}/outreach", {**BODY, "extra": 1}).status_code == 422
    assert post(client, f"/api/plans/{pid_nophoto}/outreach", {**BODY, "category": "photographer"}).status_code in (400, 422)
    assert post(client, "/api/plans/nope/outreach", BODY).status_code in (400, 404)
    assert post(client, f"/api/plans/{pid}/outreach/xyz/sent", {}).status_code in (400, 404, 422)
    r = client.post(f"/api/plans/{pid}/outreach", data=json.dumps(BODY), headers={"Content-Type": "application/json"})
    assert r.status_code in (400, 403)


def test_cli_prints_the_draft_and_sends_nothing(em, pid, tmp_path, capsys):
    from eventmate.__main__ import main
    code = main(["--plan-dir", str(tmp_path / "p"), "--log-dir", str(tmp_path / "l"), "outreach", pid, "caterer", "whatsapp", "9876543210", "--name", "Asha", "--json"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["link"].startswith("https://wa.me/919876543210?text=")
    code = main(["--plan-dir", str(tmp_path / "p"), "--log-dir", str(tmp_path / "l"), "outreach", pid, "caterer", "email", "bad address", "--name", "Asha"])
    assert code != 0


@pytest.mark.parametrize("brief,phrase", [
    ("Plan a 120-person engagement party in Pune on December 12 under INR 200,000. Venue and catering.", "an engagement party"),
    ("Plan a 120-person wedding in Pune on December 12 under INR 500,000. Venue and catering.", "a wedding"),
    ("Plan a 60-person birthday in Pune on December 12 under INR 150,000. Venue and catering.", "a birthday"),
    ("Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering.", "a community event"),
    ("Plan a 50-person gathering in Pune on December 12 under INR 100,000. Venue and catering.", "an event"),
])
def test_the_request_reads_naturally_for_every_event_type(tmp_path, brief, phrase):
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="w", require_symbolic=False)
    plan_id = e.plan(brief, today=TODAY).plan.plan_id
    d = e.draft_outreach(plan_id, C.CATERER, "email", "sales@vendor.test", "Asha")
    assert f"I am organising {phrase} in Pune" in d.body and f"for {phrase} on 2026-12-12" in d.subject
    assert " a engagement" not in d.body + d.subject and " a other" not in d.body + d.subject and "a community in" not in d.body
