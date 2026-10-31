"""The docs and the UI must not claim more than the matching can show: stop requests and delivery notices are matched on forgeable mail headers (ADR-049)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_the_adr_says_the_matching_is_not_authentication_and_how_a_forgery_is_bounded():
    adr = read("docs", "DECISIONS.md")
    tail = adr[adr.index("## ADR-049"):]
    assert "None of the matching is authentication" in tail and "can be forged" in tail and "fails safe" in tail and "only the user lifts it" in tail
    assert "Message-ID" in tail and "acknowledg" in tail and "remote server's own cleaned words" in tail


def test_the_readme_does_not_call_stop_or_bounce_matching_authenticated():
    readme = read("README.md")
    assert "not authenticated" in readme and "can be forged" in readme
    for claim in ("verified stop", "authenticated stop", "authenticated bounce", "verified bounce", "confirmed by the mail server"):
        assert claim not in readme.lower()


def test_the_ui_says_so_next_to_the_list():
    js = read("eventmate", "web", "static", "app.js")
    assert "Neither is authenticated: mail headers can be forged" in js and "remote mail server's own words, not verified" in js


def test_the_demo_states_that_a_discovered_quote_does_not_update_the_plan():
    demo = read("docs", "DEMO.md")
    assert "does not update the plan" in demo and "Re-price this plan" in demo and "not one of the plan's vendors" in demo and "promotion step that does not exist yet" in demo
    assert "TEST VENDOR" in demo and "synthetic" in demo  # which vendors are which is also stated
