"""Contact extraction from a business's own pages (ADR-049): mailto links and visible addresses only, same domain only, robots honoured, nothing guessed."""

import pytest
from pydantic import ValidationError

from eventmate.integrations.web_access import FetchError, RawPage, SafeFetcher
from eventmate.schemas import ContactCandidate
from eventmate.sourcing.contacts import find_contacts

SITE = "https://sharma-caterers.example/"


class FakeFetcher:
    """Serves canned HTML and records every address asked for."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def fetch_html(self, url):
        self.asked.append(url)
        if url not in self.pages:
            raise FetchError("the page answered HTTP 404")
        return RawPage(url=url, title="", text="", html=self.pages[url])


def page(body):
    return f"<html><head><title>x</title></head><body>{body}</body></html>"


def values(res):
    return {f["value"]: f for f in res.found}


# ---- what is extracted

def test_a_mailto_link_gives_a_candidate_with_its_source():
    f = FakeFetcher({SITE: page('<a href="mailto:Sales@Sharma-Caterers.example?subject=Hello">Email us</a>')})
    res = find_contacts(f, SITE)
    c = values(res)["sales@sharma-caterers.example"]
    assert c["channel"] == "email" and c["source_kind"] == "page_mailto" and c["source_url"] == SITE and c["flags"] == ()


def test_an_address_printed_in_the_text_is_found_without_trailing_punctuation():
    f = FakeFetcher({SITE: page("<p>Bookings: <b>orders@sharma-caterers.example</b>, or call us.</p><p>Write to events@sharma-caterers.example.</p>")})
    res = find_contacts(f, SITE)
    assert set(values(res)) == {"orders@sharma-caterers.example", "events@sharma-caterers.example"} and all(v["source_kind"] == "page_text" for v in res.found)


def test_the_same_address_as_a_link_and_as_text_is_one_candidate_and_prefers_the_link():
    f = FakeFetcher({SITE: page('<a href="mailto:hi@sharma-caterers.example">hi@sharma-caterers.example</a> and HI@Sharma-Caterers.example')})
    res = find_contacts(f, SITE)
    assert len(res.found) == 1 and res.found[0]["source_kind"] == "page_mailto"


def test_nothing_on_the_page_means_nothing_is_offered_and_nothing_is_guessed():
    f = FakeFetcher({SITE: page("<p>Welcome to Sharma Caterers. Call 020 5555 0000.</p>")})
    res = find_contacts(f, SITE)
    assert res.found == [] and any("no public email" in n for n in res.notes)
    assert not any("info@" in x or "contact@" in x for x in f.asked)  # never tries info@<domain>


# ---- hostile pages

@pytest.mark.parametrize("html", [
    '<script>var e="evil@x.example"; // a@b.example</script>',
    "<style>.a{content:'evil@x.example'}</style>",
    "<!-- evil@x.example -->",
    '<div style="display:none">evil@x.example</div>',
    '<div hidden>evil@x.example</div>',
    '<span aria-hidden="true">evil@x.example</span>',
    '<p style="visibility: hidden">evil@x.example</p>',
    '<div style="font-size:0">evil@x.example</div>',
    '<noscript>evil@x.example</noscript><template>evil@x.example</template>',
    '<a style="display:none" href="mailto:evil@x.example">x</a>',
    '<meta name="author" content="evil@x.example">',
    '<img alt="evil@x.example" src="a.png">',
    '<input type="hidden" value="evil@x.example">',
])
def test_text_a_visitor_cannot_see_is_never_an_address(html):
    res = find_contacts(FakeFetcher({SITE: page(html)}), SITE)
    assert res.found == []


def test_a_nested_hidden_block_hides_everything_inside_it_and_visible_text_after_it_still_counts():
    f = FakeFetcher({SITE: page('<div style="display:none"><p>a@x.example</p><div>b@x.example</div></div><p>real@sharma-caterers.example</p>')})
    assert set(values(find_contacts(f, SITE))) == {"real@sharma-caterers.example"}


@pytest.mark.parametrize("bad", [
    "mailto:a@b.example%0ABcc:x@y.example", "mailto:a@b.example%0D%0ABcc:x@y.example", "mailto:a b@c.example", "mailto:<a@b.example>", "mailto:a@b", "mailto:@b.example",
    "mailto:a@b.example;c@d.example", "mailto:" + "x" * 80 + "@b.example", "mailto:a@b.example\nBcc: x@y.example", "mailto:logo@2x.png", "mailto:",
])
def test_a_malformed_or_injecting_mailto_is_dropped(bad):
    assert find_contacts(FakeFetcher({SITE: page(f'<a href="{bad}">x</a>')}), SITE).found == []


def test_a_mailto_with_a_list_gives_only_each_valid_address_without_parameters():
    f = FakeFetcher({SITE: page('<a href="mailto:one@sharma-caterers.example,two@sharma-caterers.example?cc=evil@x.example&bcc=evil2@x.example">x</a>')})
    assert set(values(find_contacts(f, SITE))) == {"one@sharma-caterers.example", "two@sharma-caterers.example"}


def test_obfuscated_addresses_are_not_decoded_and_non_ascii_domains_are_not_accepted():
    body = "<p>info [at] sharma-caterers [dot] example</p><p>info(at)sharma-caterers.example</p><p>info@shöp.example</p><p>info@sharma-caterers</p>"
    assert find_contacts(FakeFetcher({SITE: page(body)}), SITE).found == []


def test_a_page_full_of_addresses_is_capped():
    body = "".join(f"<p>person{i}@sharma-caterers.example</p>" for i in range(2000))
    res = find_contacts(FakeFetcher({SITE: page(body)}), SITE)
    assert len(res.found) == 5 and any("more than" in n for n in res.notes)


def test_a_malformed_page_does_not_crash_the_extractor():
    for html in ("<<<<>>>> <a href=", "<div><p></div></p><a href='mailto:x@sharma-caterers.example'", "\x00\x01", "<a href=\"mailto:ok@sharma-caterers.example\">x" * 500):
        find_contacts(FakeFetcher({SITE: html}), SITE)


# ---- flags

def test_a_free_mail_address_and_a_different_domain_are_flagged_not_hidden():
    f = FakeFetcher({SITE: page("<p>sharma.catering@gmail.com</p><p>agent@other-agency.example</p><p>ok@sharma-caterers.example</p><p>mail@events.sharma-caterers.example</p>")})
    v = values(find_contacts(f, SITE))
    assert "generic mailbox, confirm it is the business's" in v["sharma.catering@gmail.com"]["flags"]
    assert "different domain from the website" in v["agent@other-agency.example"]["flags"]
    assert v["ok@sharma-caterers.example"]["flags"] == () and v["mail@events.sharma-caterers.example"]["flags"] == ()  # a sub-domain of the site is the same business


def test_a_free_mail_address_is_also_a_different_domain():
    v = values(find_contacts(FakeFetcher({SITE: page("<p>owner@outlook.com</p>")}), SITE))["owner@outlook.com"]
    assert len(v["flags"]) == 2


# ---- which pages are read

def test_one_same_domain_contact_page_is_read_and_its_addresses_are_attributed_to_that_page():
    pages = {SITE: page('<a href="/contact-us">Contact</a><a href="/menu">Menu</a>home@sharma-caterers.example'), "https://sharma-caterers.example/contact-us": page("<p>desk@sharma-caterers.example</p>")}
    f = FakeFetcher(pages)
    res = find_contacts(f, SITE)
    assert f.asked == [SITE, "https://sharma-caterers.example/contact-us"] and values(res)["desk@sharma-caterers.example"]["source_url"].endswith("/contact-us")
    assert values(res)["home@sharma-caterers.example"]["source_url"] == SITE


def test_a_link_to_another_domain_is_never_followed():
    pages = {SITE: page('<a href="https://evil.example/contact">Contact</a><a href="//evil.example/about">About</a><a href="http://sharma-caterers.example.evil.example/contact">Contact</a>'), "https://evil.example/contact": page("<p>x@evil.example</p>")}
    f = FakeFetcher(pages)
    res = find_contacts(f, SITE)
    assert f.asked == [SITE] and res.found == []


def test_at_most_two_pages_are_read_however_many_contact_links_there_are():
    links = "".join(f'<a href="/contact{i}">Contact {i}</a>' for i in range(10))
    pages = {SITE: page(links)} | {f"https://sharma-caterers.example/contact{i}": page(f"<p>p{i}@sharma-caterers.example</p>") for i in range(10)}
    f = FakeFetcher(pages)
    find_contacts(f, SITE)
    assert len(f.asked) == 2


def test_the_contact_page_is_preferred_over_the_about_page_and_a_failed_second_page_is_tolerated():
    pages = {SITE: page('<a href="/about">About us</a><a href="/contact">Contact</a>a@sharma-caterers.example')}
    f = FakeFetcher(pages)
    res = find_contacts(f, SITE)
    assert f.asked[1].endswith("/contact") and set(values(res)) == {"a@sharma-caterers.example"} and any("could not be read" in n for n in res.notes)


def test_an_unreadable_or_unsafe_website_yields_nothing_and_says_why():
    f = FakeFetcher({})
    res = find_contacts(f, SITE)
    assert res.found == [] and res.pages == [] and any("could not be read" in n for n in res.notes)
    for bad in ("javascript:alert(1)", "ftp://x.example/", "http://localhost/", "", "file:///etc/passwd", "https://" + "a" * 3000):
        assert find_contacts(SafeFetcher(transport=lambda *a, **k: (200, {}, b""), resolver=lambda h: ["93.184.216.34"]), bad).found == []


# ---- through the real safe fetcher: robots and size are enforced by it

def transport(pages, robots=None, log=None):
    def t(url, ip, headers, timeout, max_bytes, total_s=25):
        if log is not None:
            log.append(url)
        if url.endswith("/robots.txt"):
            return (200, {"content-type": "text/plain"}, robots.encode()) if robots is not None else (404, {}, b"")
        body = pages.get(url)
        return (200, {"content-type": "text/html; charset=utf-8"}, body.encode()) if body is not None else (404, {}, b"")
    return t


def test_robots_txt_is_honoured_for_the_contact_page_through_the_real_fetcher():
    log = []
    pages = {SITE: page('<a href="/contact">Contact</a>home@sharma-caterers.example'), "https://sharma-caterers.example/contact": page("secret@sharma-caterers.example")}
    f = SafeFetcher(transport=transport(pages, robots="User-agent: *\nDisallow: /contact\n", log=log), resolver=lambda h: ["93.184.216.34"])
    res = find_contacts(f, SITE)
    assert set(values(res)) == {"home@sharma-caterers.example"} and "https://sharma-caterers.example/contact" not in log
    assert any("could not be read" in n for n in res.notes)


def test_a_site_that_disallows_everything_gives_nothing():
    f = SafeFetcher(transport=transport({SITE: page("a@sharma-caterers.example")}, robots="User-agent: *\nDisallow: /\n"), resolver=lambda h: ["93.184.216.34"])
    assert find_contacts(f, SITE).found == []


def test_a_private_address_is_refused_by_the_safe_fetcher():
    f = SafeFetcher(transport=transport({}), resolver=lambda h: ["10.0.0.5"])
    res = find_contacts(f, SITE)
    assert res.found == [] and any("could not be read" in n for n in res.notes)


# ---- a candidate cannot exist without a source

@pytest.mark.parametrize("bad", [{"source_url": ""}, {"source_url": "javascript:alert(1)"}, {"source_url": "ftp://x.example/a"}, {"value": "not-an-address"}, {"value": "a@b.example,c@d.example"},
                                  {"status": "confirmed"}, {"surprise": 1}, {"vendor_id": "cat-pune-rasoi"}, {"source_kind": "guessed"}, {"channel": "sms"}])
def test_a_contact_candidate_needs_a_source_a_valid_address_and_a_real_state(bad):
    from datetime import datetime, timezone

    from eventmate.schemas import external_vendor_id

    base = dict(candidate_id="c1", vendor_id=external_vendor_id("osm:node/1"), channel="email", value="a@b.example", source_url="https://b.example/", source_kind="page_mailto", found_at=datetime.now(timezone.utc))
    ContactCandidate(**base)
    with pytest.raises(ValidationError):
        ContactCandidate(**{**base, **bad})
