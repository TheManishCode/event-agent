"""Browser end-to-end test of the web UI (skipped unless Playwright and a Chromium binary exist)."""

import shutil
import threading

import pytest

pw = pytest.importorskip("playwright.sync_api")
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome-stable")
pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="no Chromium binary found")

from werkzeug.serving import make_server  # noqa: E402

from eventmate.service import EventMate  # noqa: E402
from tests.conftest import EXAMPLE, TODAY  # noqa: E402
from tests.test_conversations import FakeChannel  # noqa: E402
from eventmate.web.app import create_app  # noqa: E402


def expected_noise(text):
    """Browser messages that are not app errors: a deliberate 4xx the test provokes, and the software-rendering performance notes of a headless browser with no GPU (a driver stall, or the viewer's own frame-budget warning, which appears when the machine is busy)."""
    return "status of 4" in text or "GL Driver Message" in text or "GPU stall" in text or "swiftshader" in text.lower() or "rAF timed out" in text


@pytest.fixture
def server(tmp_path):
    em = EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="ui")
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY), threaded=True)  # pinned clock: seed dates are in 2026
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_full_user_journey_without_console_errors(server):
    problems = []
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") and not expected_noise(m.text) else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.goto(server)

        # build a plan from free text
        page.click(".classic summary")
        page.fill("#request-text", EXAMPLE)
        page.click("#plan-button")
        page.wait_for_selector("#hero h1", timeout=60000)
        import re

        m = re.search(r"(\d+)/(\d+) hard constraints passed", page.inner_text(".badges"))
        assert m and m.group(1) == m.group(2) and int(m.group(1)) > 20  # every check passed, whatever the count

        # dietary advisories are shown, labelled as unreviewed drafts
        page.wait_for_selector("#advisories li", timeout=30000)
        assert "unreviewed draft" in page.inner_text("#advisories") and "do not change the plan" in page.inner_text("#advisories")

        # every tab renders
        for tab in ("timeline", "vendors", "budget", "audit", "overview"):
            page.click(f"#tab-{tab}")
            page.wait_for_selector(f"#panel-{tab}")
        page.click("#tab-vendors")
        assert page.locator(".alt").count() >= 3  # alternatives are always shown

        # the Why? panel shows the hard constraints checked for the pick, with confidence and source
        page.click(".vendor.venue button:has-text('Why this vendor?')")
        page.wait_for_selector(".vendor.venue .why .check.pass", timeout=30000)
        assert page.locator(".vendor.venue .why .check.pass").count() >= 3
        assert page.locator(".vendor.venue .why .check.fail").count() == 0
        assert "confidence" in page.inner_text(".vendor.venue .why").lower()

        # confirm a booking, then change the budget with a value that is not a multiple of any step
        page.click(".vendor.venue button:has-text('I confirmed')")
        page.wait_for_selector("text=Confirmed by you", timeout=30000)
        page.click("button:has-text('Something else changed')")
        page.fill("#f-budget", "190000")
        page.click("#replan-submit")
        page.wait_for_selector("#tab-changes[aria-selected=true]", timeout=30000)
        page.wait_for_selector(".dcol.preserved")
        assert "confirmed booking kept" in page.inner_text(".dcol.preserved")
        assert page.locator("#version-select option").count() == 3

        # the ledger records a user-reported payment and totals it
        page.click("#tab-budget")
        page.select_option("#l-kind", "payment")
        page.fill("#l-amount", "12000")
        page.click("#l-add")
        page.wait_for_selector("#l-paid-total:has-text('12,000')", timeout=30000)

        # the weather card explains itself when no key is configured (no network is used)
        page.click("#tab-overview")
        page.wait_for_selector("#weather-msg:has-text('not configured')", timeout=30000)
        page.click("#tab-budget")

        # a recorded quote reprices the plan after the user re-prices; vendor confirmation changes the badge
        page.fill("#q-amount", "400")
        page.select_option("#q-basis", "per_plate")
        page.fill("#q-guests", "100")
        page.fill("#q-expires", "2027-01-15")
        page.fill("#q-by", "Asha")
        page.select_option("#q-source", "user_input")
        page.select_option("#q-vendor", index=1)
        page.click("#q-add")
        page.wait_for_selector("#q-stale", timeout=30000)
        page.click("#q-reprice")
        page.wait_for_selector("#q-stale", state="detached", timeout=30000)
        for _ in range(2):  # the form re-renders (and clears) after every save; the first try has no evidence and is refused
            page.select_option("#q-vendor", index=0)  # the venue: the one booking the user confirmed above
            page.select_option("#q-source", "vendor_confirmation")
            page.fill("#q-amount", "400")
            page.fill("#q-expires", "2027-01-15")
            page.fill("#q-by", "Asha")
            if _:
                page.fill("#q-evidence", "whatsapp msg 4411")
            page.click("#q-add")
            if not _:
                page.wait_for_selector("#q-msg:has-text('evidence')", timeout=30000)
        page.click("#tab-vendors")
        page.wait_for_selector(".vendor.venue .conf-badge:has-text('Confirmed by vendor')", timeout=30000)
        assert page.locator(".vendor.caterer .conf-badge:has-text('Confirmed by vendor')").count() == 0

        # a conflicting date asks for a decision and changes nothing
        page.click("#tab-vendors")
        page.click("button:has-text('Something else changed')")
        page.click("label:has-text('Date change') span")
        page.fill("#f-date", "2026-12-19")
        page.click("#replan-submit")
        page.wait_for_selector("text=This needs your decision", timeout=30000)
        assert "Nothing was changed" in page.inner_text("#replan-result")
        page.click("#replan-close")
        assert page.locator("#version-select option").count() == 4
        browser.close()
    assert problems == []


def test_page_has_no_inline_script_or_style_and_blocks_framing(server):
    import urllib.request

    with urllib.request.urlopen(server + "/") as r:
        html = r.read().decode()
        csp = r.headers["Content-Security-Policy"]
    assert "<script>" not in html and " style=" not in html and "onclick=" not in html
    assert "frame-ancestors 'none'" in csp


def test_hostile_vendor_names_render_as_inert_text(tmp_path):
    """A vendor record containing markup must never execute in the UI."""
    import json

    from eventmate.agents.vendor import DATA_DIR, VendorRepository

    evil = '<img src=x onerror="window.__pwned=1">'
    doc = json.loads((DATA_DIR / "seed_venues.json").read_text())
    for r in doc["records"]:
        if r["id"] in ("ven-pune-orchid", "ven-pune-heritage"):
            r["name"] = evil
    path = tmp_path / "v.json"
    path.write_text(json.dumps(doc))
    em = EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", repo=VendorRepository(venues_path=path))
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    dialogs = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page()
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.click(".classic summary")
            page.fill("#request-text", EXAMPLE)
            page.click("#plan-button")
            page.wait_for_selector("#hero h1", timeout=60000)
            for tab in ("vendors", "audit", "overview"):
                page.click(f"#tab-{tab}")
                page.wait_for_selector(f"#panel-{tab}")
            page.click("#tab-vendors")
            assert evil in page.inner_text("#panel-vendors")  # shown literally, as text
            assert page.evaluate("window.__pwned") is None
            assert page.locator("img[src='x']").count() == 0
            browser.close()
    finally:
        srv.shutdown()
    assert dialogs == []


def test_failed_plan_offers_verified_tradeoffs_and_applying_one_builds_a_plan(server):
    problems = []
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") and not expected_noise(m.text) else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.goto(server)
        page.click(".classic summary")
        page.fill("#request-text", EXAMPLE.replace("200,000", "150,000"))
        page.click("#plan-button")
        page.wait_for_selector("#tradeoff-button", timeout=60000)
        page.click("#tradeoff-button")
        page.wait_for_selector(".option", timeout=120000)
        assert page.locator(".option").count() >= 2
        assert "Verified" in page.inner_text(".option")
        page.click(".option >> nth=0 >> button:has-text('Apply this option')")
        page.wait_for_selector("#hero h1", timeout=60000)
        assert "trade-off chosen by the user" in page.inner_text("#hero")
        assert problems == []


def test_chat_intake_review_card_and_confirmation(server):
    problems = []
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") and not expected_noise(m.text) else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.goto(server)

        # a half-given brief: the AI asks, nothing can be confirmed yet
        page.fill("#chat-input", "birthday in Pune, 40 guests, 90k budget")
        page.click("#chat-send")
        page.wait_for_selector("#review .val.todo", timeout=30000)
        assert page.locator("#review .btn.primary").is_disabled() and "date" in page.inner_text("#review .btn.primary")

        # the user answers in words; the review card is one sentence with every value visible
        page.fill("#chat-input", "on December 12")
        page.click("#chat-send")
        page.wait_for_selector("#review .btn.primary:not([disabled])", timeout=30000)
        card = page.inner_text("#review")
        assert "40 guests" in card and "Pune" in card and "90,000" in card and "12 Dec 2026" in card

        # the human can correct a value inline before approving; nothing is planned until they confirm
        page.click("#review [data-field=guest_count]")
        page.fill("#review input", "45")
        page.keyboard.press("Enter")
        page.wait_for_selector("#review [data-field=guest_count]:has-text('45 guests')")
        assert page.locator("#plan-list li").count() == 0

        page.click("#review .btn.primary")
        page.wait_for_selector("#hero h1", timeout=60000)
        assert "45 guests" in page.inner_text("#hero")
        page.click("#tab-audit")
        assert not problems, problems


def _serve(em, assistant=None):
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, assistant=assistant), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_ask_tab_answers_with_sources_and_changes_need_a_click(tmp_path):
    from eventmate.assistant.agent import Assistant
    from tests.test_assistant import Scripted, answer, tool

    em = EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="ask")
    pid = em.plan(EXAMPLE, today=TODAY).plan.plan_id
    model = Scripted(tool("propose_ledger_entry", kind="payment", category="venue", amount=5000, note="advance"), answer("I filed that for your approval.", "propose_ledger_entry"))
    srv = _serve(em, Assistant(em, {"large": model, "small": model}))
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") and not expected_noise(m.text) else None)
            page.on("pageerror", lambda e: problems.append(str(e)))
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#tab-ask")

            # a routine question is answered from the plan with no AI, and says so
            page.fill("#ask-input", "how much is left in the budget?")
            page.click("#panel-ask button[type=submit]")
            page.wait_for_selector(".bubble.answer", timeout=30000)
            assert "61,000" in page.inner_text(".bubble.answer") and "no AI needed" in page.inner_text(".bubble.answer")
            page.click(".why-trace summary")
            page.wait_for_selector(".trace li", timeout=30000)
            assert "route" in page.inner_text(".trace") and "tool" in page.inner_text(".trace")

            # the organiser can approve an answer once; the same question then reuses it with no AI, and feedback is one click
            page.click(".bubble.answer button:has-text('Helpful')")
            page.wait_for_selector(".bubble.answer .answer-meta :text('Thanks, noted.')", timeout=30000)
            page.click(".bubble.answer button:has-text('Save as approved answer')")
            page.wait_for_selector("#approved-answers .proposal", timeout=30000)
            page.fill("#ask-input", "how much is left in the budget?")
            page.click("#panel-ask button[type=submit]")
            page.locator(".bubble.answer").nth(1).wait_for(timeout=30000)
            assert "approved answer, no AI needed" in page.locator(".bubble.answer").nth(1).inner_text()

            # a change the assistant suggests waits for a human click and changes nothing before it
            page.fill("#ask-input", "please record an advance payment of 5000 for the venue")
            page.click("#panel-ask button[type=submit]")
            page.wait_for_selector("#pending-approvals .proposal", timeout=30000)
            assert "5,000" in page.inner_text("#pending-approvals") and em.ledger_entries(pid) == []
            page.click("#pending-approvals button:has-text('Approve')")
            page.wait_for_selector("#toast:not([hidden])", timeout=30000)
            assert len(em.ledger_entries(pid)) == 1
            assert not problems, problems
    finally:
        srv.shutdown()


def test_explore_view_runs_every_tool_from_forms_and_free_text_without_spending_anything(tmp_path):
    """Deals, places, trip, weather and the free-text box, all against fakes: no network, no credits, no console errors."""
    import json

    from eventmate.integrations.deals import DealAgent
    from tests.test_deals import A, B, C, D, E, PAGES, FakeFetch, FakeSearch
    from tests.test_discovery import src as osm_src
    from tests.test_places import place, src as places_src, Net as PlacesNet
    from tests.test_tripsearch import Fake as FlightFake, flat
    from tests.test_weather import src as wx_src

    evil = '<img src=x onerror="window.__pwned=1">'
    em = EventMate(
        log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="x", flights=FlightFake(flat()), weather=wx_src(), discovery=osm_src(),
        places=places_src(PlacesNet([place("Hall A", 4.6, 500), place(evil, 4.9, 3)])),
    )
    em.deals_factory = lambda on_call: DealAgent(FakeSearch([A, B, C, D, E]), FakeFetch(PAGES), on_call=on_call)

    class Router:
        name, model = "scripted", "m"

        def complete(self, system, user, max_tokens):
            return json.dumps({"action": "deals", "kind": "caterer", "city": "Pune", "guests": 100, "budget_inr": 60000})

    em.router_clients = lambda: [Router()]
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems, dialogs = [], []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: problems.append(str(e)))
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.click("#explore-open")
            page.wait_for_selector("#x-text")
            page.wait_for_selector("#x-status:has-text('own words')")

            page.fill("#x-text", "cheapest caterers in Pune for 100 guests under 60k")
            page.click("#x-go")
            page.wait_for_selector("#routed-out #deals-out", timeout=30000)
            text = page.inner_text("#deals-out")
            assert "not a quote" in text and "unusually low" in text and "fits budget" in text and "Understood as: deals" in page.inner_text("#routed-out")

            page.select_option("#p-kind", "venue")
            assert page.locator(".save-lead").count() == 0  # no plan open: nothing to attach a lead to
            page.click("#p-go")
            page.wait_for_selector("#places-out", timeout=30000)
            assert "Hall A" in page.inner_text("#places-out") and "Google" in page.inner_text("#places-out")
            assert "onerror" not in page.inner_text("#places-out")  # the hostile place name is stripped server-side and skipped

            page.fill("#t-start", "2026-12-12")
            page.click("#t-go")
            page.wait_for_selector("#trip-out .trip", timeout=30000)
            trip = page.inner_text("#trip-out")
            assert "Cheapest" in trip and "Best Value" in trip and "not a multi-city fare" in trip and "DEL to BOM" in trip

            page.fill("#w-date", "2026-11-02")
            page.click("#w-go")
            page.wait_for_selector("#weather-out:has-text('light rain')", timeout=30000)

            page.fill("#d-city", 'Pune"; x')
            page.click("#d-go")
            page.wait_for_selector("#x-error", timeout=30000)
            assert page.evaluate("window.__pwned") is None and page.locator("img[src='x']").count() == 0

            page.click("#explore-open")  # toggles back to the plan view / empty state
            page.wait_for_selector("#explore-view", state="hidden")
            browser.close()
    finally:
        srv.shutdown()
    assert dialogs == []
    assert [x for x in problems if not expected_noise(x)] == []


def test_saving_a_deal_as_a_lead_shows_it_in_the_budget_tab_without_changing_prices(tmp_path):
    from eventmate.integrations.deals import DealAgent
    from tests.test_deals import A, B, C, D, E, PAGES, FakeFetch, FakeSearch

    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="lead")
    em.deals_factory = lambda on_call: DealAgent(FakeSearch([A, B, C, D, E]), FakeFetch(PAGES), on_call=on_call)
    pid = em.plan(EXAMPLE, today=TODAY).plan.plan_id
    before = em.store.latest(pid).total_cost
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#explore-open")
            page.click("#d-go")
            page.wait_for_selector("#deals-out .save-lead", timeout=30000)
            page.locator(".save-lead").first.click()
            page.wait_for_selector(".save-lead:has-text('Saved')", timeout=30000)
            page.click("#explore-open")
            page.click("#tab-budget")
            page.wait_for_selector("#leads .lead", timeout=30000)
            assert "not a quote" in page.inner_text("#leads")
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []
    assert em.store.latest(pid).total_cost == before and len(em.leads(pid)) == 1


def test_manual_path_when_no_channel_is_set_up_link_then_i_sent_it(tmp_path):
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="out", channels={"email": FakeChannel("email", False, False), "whatsapp": FakeChannel("whatsapp", False, False)})
    pid = em.plan(EXAMPLE, today=TODAY).plan.plan_id
    before = em.store.latest(pid).total_cost
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, poll_seconds=0), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#tab-talk")
            page.wait_for_selector("#o-make", timeout=30000)
            assert "not set" in page.inner_text("#o-channels")
            page.fill("#o-to", "98765 43210")
            page.click("#o-make")
            page.wait_for_selector("#o-msg:not(:empty)", timeout=30000)  # no name yet: refused with a reason
            page.fill("#o-by", "Asha")
            page.click("#o-make")
            page.wait_for_selector(".thread a[href^='https://wa.me/919876543210']", timeout=30000)
            assert "budget" not in page.inner_text(".thread").lower() and page.locator(".approve").count() == 0
            page.click(".thread button:has-text('I sent it')")
            page.wait_for_selector(".msg.out .tag:has-text('you sent it')", timeout=30000)
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []
    assert em.store.latest(pid).total_cost == before and [d.status for d in em.outreach(pid)] == ["marked_sent"]


def test_agent_conversation_in_the_browser_approve_send_reply_and_record_as_quote(tmp_path):
    from datetime import datetime, timedelta, timezone

    from eventmate.messaging.channels import Inbound
    from eventmate.schemas import VendorCategory as C

    chans = {"email": FakeChannel("email"), "whatsapp": FakeChannel("whatsapp")}
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="conv", channels=chans)
    pid = em.plan(EXAMPLE, today=TODAY).plan.plan_id
    chosen = em.store.latest(pid).selection_for(C.CATERER).vendor_id
    before = em.store.latest(pid).total_cost
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, poll_seconds=0), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#tab-talk")
            page.wait_for_selector("#c-save", timeout=30000)
            assert "ready" in page.inner_text("#o-channels")
            page.select_option("#c-vendor", chosen)
            page.fill("#c-email", "sales@annapurna.in")
            page.select_option("#c-pref", "email")
            page.click("#c-save")
            page.wait_for_selector("#c-list", timeout=30000)
            page.fill("#o-by", "Asha")
            page.click("#o-all")
            page.wait_for_selector(".msg.out[data-status='prepared'] button.approve", timeout=30000)
            assert chans["email"].sent == []  # a proposal sends nothing
            assert "Approve" in page.inner_text(".msg.out") and page.inner_text("#tab-talk").endswith("(1)")  # the tab badge counts what waits for you
            page.click("button.approve")
            page.wait_for_selector(".msg.out[data-status='sent']", timeout=30000)
            assert len(chans["email"].sent) == 1
            chans["email"].inbox = [Inbound(channel="email", sender="sales@annapurna.in", external_id="<r1@v>", received_at=datetime.now(timezone.utc) + timedelta(minutes=2),
                                           body="Yes available. Rs 650 per plate. <script>window.__pwned=1</script>")]
            page.click("#o-poll")
            page.wait_for_selector(".msg.in", timeout=30000)
            assert "Rs 650" in page.inner_text(".msg.in") and "They wrote INR 650" in page.inner_text(".msg.in") and "Not recorded as a quote" in page.inner_text(".msg.in")
            assert page.evaluate("window.__pwned") is None
            page.click("button:has-text('Record as quote')")
            page.wait_for_selector("#q-amount", timeout=30000)
            assert page.input_value("#q-amount") == "650" and page.input_value("#q-basis") == "per_plate" and "email reply" in page.input_value("#q-evidence")
            assert em.observations.quotes(chosen) == []  # prefilled, not recorded
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []
    assert em.store.latest(pid).total_cost == before


def test_vendor_scores_card_shows_the_formula_for_recorded_quotes(tmp_path):
    from datetime import date, datetime, timezone

    from eventmate.schemas import VendorCategory as C

    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="score")
    pid = em.plan(EXAMPLE, today=TODAY).plan.plan_id
    before = em.store.latest(pid).total_cost
    for vendor, amount in (("cat-pune-annapurna", 500), ("cat-pune-rasoi", 600), ("cat-pune-spice", 700)):
        em.observe_quote(vendor, C.CATERER, source_type="vendor_quote", entered_by="asha", observed_at=datetime.now(timezone.utc), expires_at=date(2026, 12, 1),
                         confidence=0.9, evidence_ref="email: quote", amount_inr=amount, basis="per_plate", guest_count=100)
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#tab-budget")
            page.wait_for_selector("#scores .score", timeout=30000)
            text = page.inner_text("#scores")
            names = em._vendor_names()
            assert text.index(names["cat-pune-annapurna"]) < text.index(names["cat-pune-spice"]) and "median" in text and "left out" not in text
            assert "cat-pune-annapurna" not in text and f"{names['cat-pune-annapurna']} (seed)" in text  # a name and where it came from, never a raw id
            assert "Response time" in text or "response time" in text
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []
    assert em.store.latest(pid).total_cost == before


def test_standing_permission_answers_a_reply_by_itself_and_the_kill_switch_stops_it(tmp_path):
    from datetime import datetime, timedelta, timezone

    from eventmate.messaging.channels import Inbound
    from eventmate.messaging.conversations import SendPolicy
    from eventmate.schemas import VendorCategory as C

    chans = {"email": FakeChannel("email"), "whatsapp": FakeChannel("whatsapp")}
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="auto", channels=chans)
    em.conversations.policy = SendPolicy(per_vendor_per_day=10, per_day=50, whatsapp_gap_s=0)
    # the sending window is 09:00-20:00 IST; pin the clock inside it so the test does not depend on the hour it runs
    fixed = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
    em.outreach_store.now = em.conversations.now = lambda: fixed
    pid = em.plan(EXAMPLE, today=TODAY).plan.plan_id
    d = em.draft_outreach(pid, C.CATERER, "email", "sales@annapurna.in", "Asha")
    em.approve_outreach(pid, d.draft_id, d.content_hash)
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, poll_seconds=0), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#tab-talk")
            page.wait_for_selector("#a-on", timeout=30000)
            assert "off" in page.inner_text("#autoreply") and page.locator("#a-kill").count() == 1
            page.click("#a-on")
            page.wait_for_selector("#o-msg:not(:empty)", timeout=30000)  # no name yet: refused with a reason
            page.fill("#o-by", "Asha")
            page.click("#a-on")
            page.wait_for_selector("#a-off", timeout=30000)
            assert "on until" in page.inner_text("#autoreply")
            chans["email"].inbox = [Inbound(channel="email", sender="sales@annapurna.in", external_id="<r1>", received_at=fixed + timedelta(minutes=5), body="Hello, yes we can do it.")]
            page.click("#o-poll")
            page.wait_for_selector(".msg.out .tag:has-text('sent automatically')", timeout=30000)
            assert len(chans["email"].sent) == 2 and "automated assistant" in chans["email"].sent[1][2]
            page.click("#a-kill")
            page.wait_for_selector("#a-resume", timeout=30000)
            assert "stopped" in page.inner_text("#autoreply")
            chans["email"].inbox = [Inbound(channel="email", sender="sales@annapurna.in", external_id="<r2>", received_at=fixed + timedelta(minutes=9), body="Noted, thank you.")]
            page.click("#o-poll")
            page.wait_for_selector(".msg.in:has-text('Noted, thank you.')", timeout=30000)
            assert len(chans["email"].sent) == 2  # the kill switch held
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []


def test_quote_job_in_the_browser_start_approve_reply_confirm_with_validity(tmp_path):
    from datetime import datetime, timedelta, timezone

    from eventmate.messaging.channels import Inbound
    from eventmate.messaging.conversations import SendPolicy

    chans = {"email": FakeChannel("email"), "whatsapp": FakeChannel("whatsapp")}
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="jobui", channels=chans, require_symbolic=False)
    em.conversations.policy = SendPolicy(per_vendor_per_day=10, per_day=50, whatsapp_gap_s=0)
    vendors = {"cat-pune-annapurna": "a@annapurna.test", "cat-pune-rasoi": "r@rasoi.test", "cat-pune-spice": "s@spice.test"}
    for vid, to in vendors.items():
        em.set_vendor_contact(vid, to, None, "email")
    em.plan(EXAMPLE, today=TODAY)
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, poll_seconds=0), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#tab-talk")
            page.wait_for_selector("#j-start", timeout=30000)
            for vid in vendors:
                page.check(f"#jobs input[value='{vid}']")
            page.click("#j-start")
            page.wait_for_selector("#o-msg:not(:empty)", timeout=30000)  # no name yet
            page.fill("#o-by", "Asha")
            page.click("#j-start")
            page.wait_for_selector(".job [data-item='approve_requests']", timeout=30000)
            assert chans["email"].sent == []  # drafts only
            page.click(".job button:has-text('Approve all requests')")
            page.wait_for_selector(".job [data-item='approve_requests']", state="detached", timeout=30000)
            assert len(chans["email"].sent) == 3
            now = datetime.now(timezone.utc)
            chans["email"].inbox = [Inbound(channel="email", sender="r@rasoi.test", external_id="<r1>", received_at=now + timedelta(minutes=2), body="Total INR 66,000 for the event. Valid for 10 days.")]
            page.click("#o-poll")
            page.wait_for_selector("[data-item='confirm_claim'] .confirm-claim", timeout=30000)
            assert "66,000" in page.inner_text("[data-item='confirm_claim']") and page.input_value(".claim-days") == "10"
            assert em.observations.quotes("cat-pune-rasoi") == []  # proposed, not recorded
            page.fill(".claim-days", "")
            page.click(".confirm-claim")
            page.wait_for_selector("#o-msg:not(:empty)", timeout=30000)  # validity is required
            assert em.observations.quotes("cat-pune-rasoi") == []
            page.fill(".claim-days", "10")
            page.click(".confirm-claim")
            page.wait_for_selector(".confirm-claim", state="detached", timeout=30000)
            q = em.observations.quotes("cat-pune-rasoi")[0]
            assert q.amount_inr == 66_000 and q.entered_by == "user_confirmed" and "10 days" in q.note
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []


def test_ideas_tab_suggests_labels_pins_and_dismisses_without_touching_the_plan(tmp_path):
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="ideasui", require_symbolic=False)
    pid = em.plan(EXAMPLE, today=TODAY).plan.plan_id
    before = (em.store.latest(pid), len(em.store.versions(pid)))
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, poll_seconds=0), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#tab-ideas")
            page.wait_for_selector("#ideas-go", timeout=30000)
            assert "Suggestions, not verified offers" in page.inner_text("#ideas-notice") and page.locator(".idea").count() == 0
            page.fill("#ideas-prefs", "rustic")
            page.click("#ideas-go")
            page.wait_for_selector(".idea", timeout=30000)
            n = page.locator(".idea").count()
            assert 6 <= n <= 10 and "INR" not in page.inner_text("#ideas") and "built-in templates" in page.inner_text("#ideas")
            page.locator(".idea .pin").first.click()
            page.wait_for_selector("#notes .note", timeout=30000)
            assert page.locator(".idea .tag:has-text('pinned')").count() == 1
            page.locator(".idea .dismiss").nth(1).click()
            page.wait_for_selector("#ideas >> text=1 dismissed", timeout=30000)
            assert page.locator(".idea").count() == n - 1 and "1 dismissed" in page.inner_text("#ideas")
            page.locator("#notes input[type=checkbox]").first.check()
            page.wait_for_selector("#notes input[type=checkbox]:checked", timeout=30000)
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []
    assert (em.store.latest(pid), len(em.store.versions(pid))) == before and em.ideas.notes(pid)[0].done


def test_community_tab_shows_checks_refuses_with_repairs_and_lets_only_the_user_mark_items(tmp_path):
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="commui", require_symbolic=False)
    pid = em.plan("Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering.", today=TODAY).plan.plan_id
    em.plan("Plan a 120-person wedding in Pune on December 12 under INR 500,000. Venue and catering.", today=TODAY)
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, poll_seconds=0), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 1000})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector(".plan-item", timeout=30000)
            page.locator(".plan-item", has_text="Wedding").click()
            page.wait_for_selector("#hero h1:has-text('Wedding')", timeout=30000)
            assert page.locator("#tab-community").count() == 0  # only community plans have the tab
            page.locator(".plan-item", has_text="Community").click()
            page.wait_for_selector("#hero h1:has-text('Community')", timeout=30000)
            page.wait_for_selector("#tab-community", timeout=30000)
            page.click("#tab-community")
            page.wait_for_selector("#c-summary", timeout=30000)
            assert page.inner_text("#c-summary") == "11 conditional items not verified"
            text = page.inner_text("#community").lower()
            import re

            assert "college fest" in text and "not reviewed" in text and "does not verify" in text and not re.search(r"\b(?:safe|compliant|compliance)\b", text)
            assert "Attendance Vs Capacity" in page.inner_text("#c-checks") and "Setup Cleanup Owners" in page.inner_text("#c-unverified")
            # a failing save shows what failed and how to fix it, and saves nothing
            page.fill("#c-own", "100000")
            page.click("#c-save")
            page.wait_for_selector("#c-errors:not([hidden])", timeout=30000)
            err = page.inner_text("#c-errors")
            assert "Funding Vs Budget" in err and "Short by INR 57,500" in err and "To fix" in err
            assert em.store.latest(pid).version == 1
            # fixed: the saved version is checked, the tab reloads on it
            page.fill("#c-own", "160000")
            page.click("#c-save")
            page.wait_for_selector("#c-summary", timeout=30000)
            page.wait_for_selector("#toast:has-text('Saved as version 2')", timeout=30000)
            assert em.store.latest(pid).version == 2 and em.store.latest(pid).request.community.own_funds_inr == 160000
            # only the user can mark an item; a name is needed
            page.click("#tab-community")
            page.wait_for_selector(".cond", timeout=30000)
            page.locator(".cond[data-item='venue_permission'] .confirm-item").click()
            page.wait_for_selector("#c-msg:not(:empty)", timeout=30000)
            assert em.community_status(pid, today=TODAY).not_verified == 11
            page.locator(".cond[data-item='venue_permission'] input[aria-label='Your name']").fill("Asha")
            page.locator(".cond[data-item='venue_permission'] input[aria-label='Note']").fill("email from the hall manager")
            page.locator(".cond[data-item='venue_permission'] .confirm-item").click()
            page.wait_for_selector("#c-summary:has-text('10 conditional items not verified')", timeout=30000)
            assert "Recorded by Asha" in page.inner_text(".cond[data-item='venue_permission']")
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []


def test_updates_inbox_and_the_two_step_email_copy_in_the_browser(tmp_path):
    from datetime import datetime, timedelta, timezone

    from eventmate.messaging.conversations import SendPolicy
    from eventmate.schemas import VendorCategory as C
    from tests.mailfake import MailWorld

    now = datetime.now(timezone.utc)
    world = MailWorld(lambda: now)
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="updui", channels={"email": world.channel(), "whatsapp": FakeChannel("whatsapp", False, False)}, require_symbolic=False)
    em.conversations.policy = SendPolicy(per_vendor_per_day=10, per_day=50, whatsapp_gap_s=0)
    pid = em.plan("Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering.", today=TODAY).plan.plan_id
    job = em.jobs.start_job(pid, C.CATERER, {"cat-pune-annapurna": ("email", "a@annapurna.test"), "cat-pune-rasoi": ("email", "r@rasoi.test")}, "2 caterer quotes", now + timedelta(days=7), "Asha", min_quotes=2)
    em.jobs.approve_requests(job.job_id)
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, poll_seconds=0), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 1000})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#tab-talk")
            page.wait_for_selector("#updates .digest", timeout=30000)
            assert "2 caterer quotes" in page.inner_text("#updates")
            page.locator("#updates .digest button:has-text('Mark read')").first.click()
            page.wait_for_selector("#updates .digest[data-unread='false']", timeout=30000)
            # the email copy is off; turning it on is two steps and shows exactly what will happen
            assert "Off." in page.inner_text("#notify") and world.sent_to("owner@personal.test") == []
            page.fill("#notify-to", "nobody")
            page.click("#notify-review")
            page.wait_for_selector("#notify-msg:not(:empty)", timeout=30000)
            page.fill("#notify-to", "owner@personal.test")
            page.check("#notify input[aria-label='confirmed amounts']")
            page.click("#notify-review")
            page.wait_for_selector("#notify-summary", timeout=30000)
            summary = page.inner_text("#notify-summary")
            assert "owner@personal.test" in summary and "confirmed amounts" in summary and "vendor message text" in summary and "budget" in summary.lower()
            assert em.notifier.status().state == "pending"  # nothing is on until the owner confirms
            page.click("#notify-confirm")
            page.wait_for_selector("#notify-state", timeout=30000)
            assert "owner@personal.test and no one else" in page.inner_text("#notify-state") and em.notifier.status().state == "on"
            page.click("#notify-off")
            page.wait_for_selector("#notify-review", timeout=30000)
            assert em.notifier.status().state == "off"
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []


def test_a_required_access_need_keeps_the_plan_provisional_everywhere_until_the_venue_answers(tmp_path):
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="accui", require_symbolic=False)
    pid = em.plan("Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering.", today=TODAY).plan.plan_id
    pid = em.update_community(pid, __import__("eventmate.schemas", fromlist=["CommunityDetails"]).CommunityDetails(subtype="college_fest", access_needs=("step_free_access",)), today=TODAY).plan.plan_id
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, poll_seconds=0), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 1000})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector(".plan-item", timeout=30000)
            page.locator(".plan-item", has_text="Community").click()
            page.wait_for_selector("#hero .badge:has-text('Needs verification: required accessibility unknown')", timeout=30000)
            hero = page.inner_text("#hero")
            assert "hard constraints passed" not in hero and "decidable checks passed" in hero  # never the plain green claim
            assert "provisional plan" in page.inner_text("#ov-verification") and "what was checked, and what is still open" in page.inner_text("#panels").lower()
            page.click("#tab-community")
            page.wait_for_selector("#c-verification", timeout=30000)
            assert "Needs verification: required accessibility unknown" in page.inner_text("#c-verification") and "Not verified" in page.inner_text("#community")
            # recording the venue's answer, with evidence, resolves it
            page.check("#a-boxes input[aria-label='Step-free access']")
            page.fill("#a-evidence", "email from the venue manager, 3 Nov")
            page.locator("#community input[aria-label='Your name']").last.fill("Asha")
            page.click("#a-save")
            page.wait_for_selector("#hero .badge:has-text('hard constraints passed')", timeout=30000)
            assert "Needs verification" not in page.inner_text("#hero")
            page.click("#tab-community")
            page.wait_for_selector("#c-verification.ok", timeout=30000)
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []


def test_find_vendors_review_confirm_job_stop_request_and_test_vendor_in_the_browser(tmp_path):
    from datetime import datetime, timedelta, timezone

    from eventmate.integrations.places import PlacesSource
    from eventmate.messaging.channels import Inbound
    from eventmate.messaging.conversations import SendPolicy
    from tests.test_places import KEY, Net, place
    from tests.test_shortlist import FakeFetcher

    chans = {"email": FakeChannel("email"), "whatsapp": FakeChannel("whatsapp", False, False)}
    net = Net([place("Sharma Caterers") | {"id": "ChIJsharma000001", "websiteUri": "https://sharma-caterers.example/"},
               place("Kapoor Food Services", uri="https://maps.google.com/?cid=2") | {"id": "ChIJkapoor000002", "websiteUri": "https://kapoor-food.example/"}])
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="fvui", channels=chans, require_symbolic=False, places=PlacesSource(api_key=KEY, transport=net))
    em.conversations.policy = SendPolicy(per_vendor_per_day=20, per_day=200, whatsapp_gap_s=0)
    em.contact_fetcher = lambda: FakeFetcher({
        "https://sharma-caterers.example/": "<html><body><a href='/contact'>Contact</a></body></html>",
        "https://sharma-caterers.example/contact": "<html><body><a href='mailto:orders@sharma-caterers.example'>Orders</a></body></html>",
        "https://kapoor-food.example/": "<html><body>kapoor.food@gmail.com</body></html>",
    })
    pid = em.plan("Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering.", today=TODAY).plan.plan_id
    before = em.store.latest(pid)
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, poll_seconds=0), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 1200})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#tab-talk")
            page.wait_for_selector("#find-vendors", timeout=30000)
            assert "anti-spam and privacy rules" in page.inner_text("#fv-note") and "does not verify them" in page.inner_text("#fv-note")
            # an unsupported source says so instead of an empty list
            page.select_option("#fv-cat", "caterer")
            page.select_option("#fv-src", "osm")
            page.click("#fv-search")
            page.wait_for_selector("#fv-msg:has-text('Not supported by this source')", timeout=30000)
            assert page.locator(".biz").count() == 0
            page.select_option("#fv-src", "places")
            page.click("#fv-search")
            page.wait_for_selector(".biz[data-vendor]", timeout=30000)
            assert page.locator(".biz").count() == 2 and "Discovered" in page.inner_text("#find-vendors") and "Google Places" in page.inner_text("#find-vendors")
            sharma = page.locator(".biz", has_text="Sharma Caterers")
            sharma.locator(".find-contacts").click()
            page.wait_for_selector(".cand", timeout=30000)
            assert "mail link" in page.inner_text(".cand") and "sharma-caterers.example/contact" in page.inner_text(".cand")
            assert page.locator(".biz", has_text="Kapoor").locator(".confirm-cand").count() == 0  # nothing found for it yet
            assert em.outreach_store.contacts() == {}
            # confirming needs a name; the user corrects the address
            sharma.locator(".cand-value").fill("events@sharma-caterers.example")
            sharma.locator(".confirm-cand").click()
            page.wait_for_selector("#fv-msg:has-text('Enter your name')", timeout=30000)
            assert em.outreach_store.contacts() == {}
            sharma.locator(".cand input[aria-label='Your name']").fill("Asha")
            sharma.locator(".confirm-cand").click()
            page.wait_for_selector("[data-confirmed]", timeout=30000)
            conf = page.inner_text("[data-confirmed]")
            assert "events@sharma-caterers.example" in conf and "edited by you" in conf and "Confirmed by Asha" in conf and "sharma-caterers.example/contact" in conf
            # the other business still has no confirmed address, so it cannot be ticked
            page.locator(".biz", has_text="Kapoor").locator(".find-contacts").click()
            page.wait_for_selector(".biz:has-text('Kapoor') .cand", timeout=30000)
            assert "generic mailbox" in page.locator(".biz", has_text="Kapoor").inner_text() and page.locator(".biz", has_text="Kapoor").locator("input[type=checkbox]").count() == 0
            # start a job for the confirmed business only
            page.fill("#o-by", "Asha")
            page.check("input[aria-label='Ask Sharma Caterers']")
            page.click("#fv-start")
            page.wait_for_selector(".job [data-item='approve_requests']", timeout=30000)
            assert chans["email"].sent == [] and "Sharma Caterers" in page.inner_text(".job") and "ext_" not in page.inner_text(".job")
            page.click(".job button:has-text('Approve all requests')")
            page.wait_for_selector(".job [data-item='approve_requests']", state="detached", timeout=30000)
            assert [s[0] for s in chans["email"].sent] == ["events@sharma-caterers.example"]
            assert page.locator(".thread [data-origin='discovered']").count() == 1
            # they reply asking us to stop: the address goes on the do-not-contact list
            sent_id = em.outreach(pid)[0].external_id
            chans["email"].inbox = [Inbound(channel="email", sender="events@sharma-caterers.example", external_id="<s1>", received_at=datetime.now(timezone.utc) + timedelta(minutes=2), body="Please stop messaging us.", in_reply_to=sent_id)]
            page.click("#o-poll")
            page.wait_for_selector("#dnc .sup", timeout=30000)
            assert "events@sharma-caterers.example" in page.inner_text("#dnc .sup") and "stop request" in page.inner_text("#dnc .sup") and "other addresses at sharma-caterers.example are not blocked" in page.inner_text("#dnc .sup")
            # only the user lifts it, with a reason
            page.locator("#dnc .sup .lift").click()
            page.wait_for_selector("#o-msg:not(:empty)", timeout=30000)  # no reason given: refused with a message
            assert em.is_suppressed("email", "events@sharma-caterers.example") is not None
            page.locator("#dnc .sup input[aria-label='Reason']").fill("they confirmed by phone it is fine")
            page.locator("#dnc .sup input[aria-label='Your name']").fill("Asha")
            page.locator("#dnc .sup .lift").click()
            page.wait_for_selector("#dnc .sup", state="detached", timeout=30000)
            assert em.is_suppressed("email", "events@sharma-caterers.example") is None
            # a test vendor needs the ownership statement and is labelled
            page.locator("#find-vendors summary").click()
            page.fill("#tv-name", "Test Caterer")
            page.fill("#tv-email", "owner@personal.test")
            page.click("#tv-add")
            page.wait_for_selector("#o-msg:has-text('own the address')", timeout=30000)  # ownership not confirmed: refused
            assert em.externals.all() and not [b for b in em.externals.all() if b.is_test]
            page.check("#tv-own")
            page.click("#tv-add")
            page.wait_for_selector(".biz:has-text('Test Caterer')", timeout=30000)
            assert "TEST VENDOR (address owned by Asha)" in page.locator(".biz", has_text="Test Caterer").inner_text()
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []
    assert em.store.latest(pid) == before  # none of this changed the plan


def test_a_bounced_address_shows_why_and_cannot_be_lifted_or_reconfirmed_until_the_reason_is_acknowledged(tmp_path):
    from eventmate.service import EventMate as EM
    from tests.test_conversations import FakeChannel as FC

    em = EM(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="bncui", channels={"email": FC("email"), "whatsapp": FC("whatsapp", False, False)}, require_symbolic=False)
    pid = em.plan("Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering.", today=TODAY).plan.plan_id
    from eventmate.schemas import VendorCategory as VC

    biz = em.add_discovered("osm:node/4001", "Sharma Caterers", "Pune", VC.CATERER, "osm", website="https://sharma-caterers.example/")
    em.shortlist.add_entry(pid, VC.CATERER, biz.vendor_id)
    em.shortlist.add_candidates(biz.vendor_id, [{"channel": "email", "value": "orders@sharma-caterers.example", "source_url": "https://sharma-caterers.example/contact", "source_kind": "page_mailto", "flags": ()}])
    em.suppressions.add("email", "orders@sharma-caterers.example", "bad_address", "<m1@x>", "system: delivery failure notice", "5.1.1: smtp; 550 no such user here")
    em.suppress_address("email", "other@shop.example", "Asha", "asked by phone")
    srv = make_server("127.0.0.1", 0, create_app(em, today_fn=lambda: TODAY, poll_seconds=0), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1300, "height": 1200})
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.goto(f"http://127.0.0.1:{srv.server_port}")
            page.wait_for_selector("#hero h1", timeout=30000)
            page.click("#tab-talk")
            page.wait_for_selector("#dnc .sup", timeout=30000)
            # confirming the bounced address as a contact: refused with the reason, then allowed after an informed second click
            cand = page.locator(".cand")
            page.fill("#o-by", "Asha")
            cand.locator("input[aria-label='Your name']").fill("Asha")
            cand.locator(".confirm-cand").click()
            page.wait_for_selector("#o-msg:has-text('remote mail server')", timeout=30000)
            assert em.outreach_store.contacts() == {} and cand.locator(".ack-bounce").count() == 1
            cand.locator(".ack-bounce").check()
            cand.locator(".confirm-cand").click()
            page.wait_for_selector("[data-confirmed]", timeout=30000)
            assert em.is_suppressed("email", "orders@sharma-caterers.example") is None and em.outreach_store.contacts()[biz.vendor_id].email == "orders@sharma-caterers.example"
            em.suppressions.add("email", "orders@sharma-caterers.example", "bad_address", "<m2@x>", "system: delivery failure notice", "5.1.1: smtp; 550 no such user here")  # it bounces again
            page.click("#tab-overview")
            page.click("#tab-talk")
            page.wait_for_selector("#dnc .sup", timeout=30000)
            row = page.locator("#dnc .sup", has_text="orders@sharma-caterers.example")
            assert "Why it failed: 5.1.1: smtp; 550 no such user here" in row.inner_text() and "remote mail server's own words, not verified" in row.inner_text()
            assert "can be forged" in page.inner_text("#dnc") and "authenticated" in page.inner_text("#dnc") and "Neither is authenticated" in page.inner_text("#dnc")
            assert page.locator("#dnc .sup", has_text="other@shop.example").locator(".ack-reason").count() == 0  # only an address that bounced asks for it
            row.locator("input[aria-label='Reason']").fill("they confirmed the mailbox exists")
            row.locator("input[aria-label='Your name']").fill("Asha")
            row.locator(".lift").click()
            page.wait_for_selector("#o-msg:has-text('remote mail server')", timeout=30000)  # refused, with the reason in the message
            assert em.is_suppressed("email", "orders@sharma-caterers.example") is not None
            row.locator(".ack-reason").check()
            row.locator(".lift").click()
            page.wait_for_selector("#dnc .sup:has-text('orders@sharma-caterers.example')", state="detached", timeout=30000)
            assert em.is_suppressed("email", "orders@sharma-caterers.example") is None
            browser.close()
    finally:
        srv.shutdown()
    assert [x for x in problems if not expected_noise(x)] == []


def until(page, expression, seconds=60):
    """Poll with evaluate: the page's CSP forbids the string evaluation that wait_for_function uses."""
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if page.evaluate(expression):
            return
        page.wait_for_timeout(200)
    raise AssertionError(f"timed out waiting for: {expression}")


def test_the_avatar_loads_reacts_and_never_breaks_the_page(server):
    problems = []
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") and not expected_noise(m.text) else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.goto(server)
        page.wait_for_selector("#chat-card model-viewer", state="attached", timeout=30000)
        until(page, "document.querySelector('#chat-card model-viewer').loaded === true")
        names = page.evaluate("document.querySelector('#chat-card model-viewer').availableAnimations")
        assert {"Idle", "Wave", "Point", "Dance"} <= set(names)
        # a reaction changes the animation and returns to idle; the page keeps working
        page.fill("#chat-input", "Birthday for 40 guests in Pune")
        page.click("#chat-send")
        page.wait_for_selector("#chat-log .bubble.ai", timeout=30000)
        until(page, "document.querySelector('#chat-card model-viewer').animationName === 'Idle'", 30)
        assert "Polytoria Avatar V3" in page.inner_text("footer, .credits") and page.locator("a[href*='sketchfab.com/3d-models/polytoria-avatar-v3-pauly']").count() == 1
        assert page.locator("#chat-card .mascot").bounding_box()["width"] <= 140  # it never grows to take over the card
        browser.close()
    assert problems == []


def test_the_decorative_avatar_never_takes_keyboard_focus(server):
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page()
        page.goto(server)
        page.wait_for_selector("#mascot model-viewer", timeout=30000)
        assert page.evaluate("document.querySelector('#mascot').inert")
        for _ in range(40):
            page.keyboard.press("Tab")
            assert not page.evaluate("!!document.activeElement.closest('#mascot')")
        browser.close()


def test_the_page_works_when_the_avatar_cannot_load(server):
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page()
        page.route("**/static/model-viewer.min.js", lambda route: route.abort())
        page.route("**/static/avatar.glb", lambda route: route.abort())
        page.goto(server)
        page.fill("#chat-input", "Birthday for 40 guests in Pune")
        page.click("#chat-send")
        page.wait_for_selector("#chat-log .bubble.ai", timeout=30000)
        assert page.locator("#chat-card .mascot").count() == 1 and page.locator("#chat-card model-viewer").count() == 0
        browser.close()
